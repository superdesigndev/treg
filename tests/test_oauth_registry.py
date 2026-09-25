"""The curated OAuth provider registry (oauth_providers.py).

BYO mode stays exactly as it was: the caller brings client_id/secret/URIs. REGISTRY mode is the
other half — name a provider and treg's own approved app supplies the credentials, requesting
only the scopes the chosen capability actually needs.
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient

from treg.config import get_settings


@pytest.fixture
def treg_google_app(monkeypatch):
    """treg's own Google client — what a deployment sets to offer registry connects."""
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "treg-google-cid")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "treg-google-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _consent_query(payload: dict) -> dict:
    return parse_qs(urlsplit(payload["consent_url"]).query)


async def test_registry_connect_uses_tregs_own_app(clients: AsyncClient, treg_google_app):
    """The caller supplies no credentials at all — that is the whole point."""
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    q = _consent_query(d)
    assert q["client_id"] == ["treg-google-cid"]
    assert q["redirect_uri"][0].endswith("/oauth/callback")
    assert q["state"] == [d["state"]]


async def test_the_broadest_capability_is_the_default(clients: AsyncClient, treg_google_app):
    """A plain Connect asks for write. Least-privilege-by-default meant most users had to connect
    twice — once for read, then again to widen it — which is worse than one honest consent screen."""
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    scope = _consent_query(d)["scope"][0]
    assert "webmasters" in scope and "webmasters.readonly" in scope


async def test_choosing_read_narrows_the_request(clients: AsyncClient, treg_google_app):
    """The choice is made BEFORE consent — a user who only wants read says so up front."""
    d = (await clients.post(
        "/oauth/start", json={"provider": "google-search-console", "capability": "read"}
    )).json()
    assert _consent_query(d)["scope"] == ["https://www.googleapis.com/auth/webmasters.readonly"]


async def test_capabilities_are_cumulative(clients: AsyncClient, treg_google_app):
    """write CONTAINS read. Otherwise picking write would silently cost you read access."""
    from treg import oauth_providers as P
    g = P.GOOGLE_SEARCH_CONSOLE
    assert set(g.scopes_for("read")) < set(g.scopes_for("write"))
    assert g.satisfied_capabilities(g.scopes_for("write")) == ["read", "write"]


@pytest.mark.parametrize(("name", "expected"), [
    (None, "google-search-console"),   # defaults to the service
    ("my-gsc", "my-gsc"),              # an explicit name still wins
])
async def test_secret_name_defaults_to_the_service(
    clients: AsyncClient, treg_google_app, name, expected
):
    body = {"provider": "google-search-console"} | ({"name": name} if name else {})
    d = (await clients.post("/oauth/start", json=body)).json()
    st = (await clients.get(f"/oauth/status/{d['state']}")).json()
    assert st["name"] == expected


async def test_unknown_provider_is_404(clients: AsyncClient, treg_google_app):
    r = await clients.post("/oauth/start", json={"provider": "nope"})
    assert r.status_code == 404
    assert "google-search-console" in r.text  # the error lists what IS known


async def test_unconfigured_provider_is_a_clear_422(clients: AsyncClient, monkeypatch):
    """A deployment that never set treg's Google client should say so, not fail mid-consent."""
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("TREG_GOOGLE_CLIENT_SECRET", "")
    get_settings.cache_clear()
    try:
        r = await clients.post("/oauth/start", json={"provider": "google-search-console"})
        assert r.status_code == 422
        assert "not configured" in r.text
        listed = {p["service"]: p for p in (await clients.get("/oauth/providers")).json()}
        assert listed["google-search-console"]["configured"] is False
    finally:
        get_settings.cache_clear()


def test_multi_method_provider_is_configured_when_any_method_is_available(monkeypatch):
    """The registry gives every client one correct provider-level availability value."""
    from treg import oauth_providers as P

    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_ID", "")
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_SECRET", "")
    monkeypatch.setenv("TREG_META_CLIENT_ID", "meta-cid")
    monkeypatch.setenv("TREG_META_CLIENT_SECRET", "meta-secret")
    get_settings.cache_clear()
    try:
        row = next(item for item in P.listing() if item["service"] == "instagram")
        methods = {item["name"]: item for item in row["authorization_methods"]}
        assert row["configured"] is True
        assert methods["instagram-login"]["configured"] is False
        assert methods["facebook-page"]["configured"] is True
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    (
        "pending", "default_capability", "page_capabilities", "page_default",
        "direct_warning", "page_warning",
    ),
    [
        (
            "instagram-login,page-messages", "page-tools",
            ["page-tools", "page-messages"], "page-tools", True, True,
        ),
        ("instagram-login", "page-messages", ["page-messages"], "page-messages", True, False),
        ("", "manage", ["page-messages"], "page-messages", False, False),
    ],
)
def test_one_review_setting_controls_the_instagram_connect_experience(
    monkeypatch, pending, default_capability, page_capabilities, page_default,
    direct_warning, page_warning,
):
    from treg import oauth_providers as P

    monkeypatch.setenv("TREG_OAUTH_REVIEW_PENDING", pending)
    get_settings.cache_clear()
    try:
        row = next(item for item in P.listing() if item["service"] == "instagram")
        methods = {item["name"]: item for item in row["authorization_methods"]}
        direct = methods["instagram-login"]
        page = methods["facebook-page"]

        assert row["connect_default_capability"] == default_capability
        assert row["permission_capabilities"] == (
            ["manage", "page-messages", "page-tools", "post", "read"]
            if page_warning else ["manage", "page-messages", "post", "read"]
        )
        assert page["capabilities"] == page_capabilities
        assert page["connect_capability"] == page_default
        assert direct["in_review"] is direct_warning
        assert page["capabilities_in_review"] == (["page-messages"] if page_warning else [])
        assert page["capability_labels"]["page-messages"] == (
            "Facebook Page tools + messages" if page_warning else "Facebook Page tools"
        )
        assert len(page["capability_details"]["page-messages"]) == (1 if page_warning else 6)
    finally:
        get_settings.cache_clear()


async def test_unknown_capability_is_422(clients: AsyncClient, treg_google_app):
    r = await clients.post(
        "/oauth/start", json={"provider": "google-search-console", "capability": "nope"}
    )
    assert r.status_code == 422
    assert "no capability" in r.text


async def test_neither_provider_nor_credentials_is_422(clients: AsyncClient):
    r = await clients.post("/oauth/start", json={"name": "x"})
    assert r.status_code == 422
    assert "provider" in r.text


def test_scope_label_falls_back_rather_than_raising():
    """Slack grants implied scopes we never asked for. Showing one raw string beats 500ing the
    whole connection page over unfamiliar copy."""
    from treg import oauth_providers as P
    assert P.scope_label("some:unknown:scope") == "some:unknown:scope"


# ---- consent-time disclosure -----------------------------------------------------------------
# The Meta app all three Meta providers share is registered as "Crewlet", a sibling product, and
# Facebook's consent screen shows only that bare app name. Without a notice on the treg side, the
# popup asks the user to authorize a product they have never heard of.

def test_a_notice_provider_always_reaches_a_pre_consent_modal():
    """A provider-level notice needs a modal before redirect. Traditional providers reach the
    capability modal; a provider with separate authorization methods reaches the method picker,
    whose listing entries carry the profile-specific notice."""
    from treg import oauth_providers as P

    for p in P.REGISTRY.values():
        if not p.consent_notice:
            continue
        if p.authorization_methods:
            assert all(
                p.profile_for_authorization(method.name).consent_notice
                for method in p.authorization_methods
            )
        else:
            assert len(p.capabilities) >= 2, p.service


def test_catalog_targets_are_exact_provider_approved_https_roots():
    from treg import oauth_providers as P

    assert P.DIFFBOT.profile_for_catalog_host("api.diffbot.com").base_url == "https://api.diffbot.com"

    unsafe = replace(
        P.DIFFBOT,
        catalog_targets=(P.CatalogTarget(
            host="api.diffbot.com", base_url="https://credentials.example/v3",
        ),),
    )
    with pytest.raises(ValueError, match="safe HTTPS base URL"):
        unsafe.profile_for_catalog_host("api.diffbot.com")
    with pytest.raises(ValueError, match="not uniquely approved"):
        P.DIFFBOT.profile_for_catalog_host("credentials.example")
