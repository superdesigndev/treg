"""API-key providers (auth_kind="key") — the marketplace's paste-a-key connect flow.

These share Slack's bring-your-own-credential path (verify → store → auto-provision), differing only
in the header or query param the key rides in. The upstream is the shared in-process ASGI app from
conftest (`/whoami` echoes; `/units` and `/units-bad` model Semrush's plain-text balance responses).
"""

from __future__ import annotations

import json
import httpx
from treg.api import app
import dataclasses
import pytest

from httpx import AsyncClient

from treg import oauth_providers as P


# ---- registry shape ----------------------------------------------------------------------
def test_key_providers_are_offerable_without_deployment_credentials():
    """The user brings the key, so treg holds no app of its own — a key provider must be offerable,
    not shown as 'not configured' the way an unset OAuth provider is."""
    keyed = [p for p in P.REGISTRY.values() if p.auth_kind == "key"]
    assert keyed
    for p in keyed:
        assert p.uses_pasted_secret is True, p.service
        assert p.is_token_kind is False, f"{p.service}: an API key is not a Slack bot token"
        assert P.is_configured(p) is True, p.service


async def test_adyntel_connect_collects_both_credentials_before_provisioning(clients, monkeypatch):
    def probe(request):
        assert request.method == "POST"
        assert request.url.path == "/facebook"
        assert json.loads(request.content) == {
            "company_domain": "treg-credential-check.invalid",
            "api_key": "own-key",
        }
        return httpx.Response(422, json={"detail": "email is required"})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        first = await clients.post(
            "/connections/token", json={"provider": "adyntel", "token": "own-key"},
        )
        assert first.status_code == 200, first.text
        connection = first.json()
        assert connection["health"] == "unknown"
        pending_tool = next(
            t for t in (await clients.get("/tools")).json() if t["name"] == "adyntel"
        )
        assert [(b["location"], b["name"]) for b in pending_tool["bindings"]] == [
            ("json", "api_key"),
        ]
        ready = await clients.post(
            f"/connections/{connection['id']}/extra-credential",
            json={"value": "owner@example.com"},
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "adyntel")
    assert [(b["location"], b["name"]) for b in tool["bindings"]] == [
        ("json", "api_key"), ("json", "email"),
    ]


def test_paid_key_verification_probe_is_typed_and_unique():
    paid = {p.service: p.probe_cost_micro for p in P.REGISTRY.values() if p.probe_cost_micro}
    assert paid == {"keenable": 4_000, "trestleiq": 15_000}
    assert all(isinstance(p.probe_cost_micro, int) and p.probe_cost_micro >= 0
               for p in P.REGISTRY.values())
    listing = {row["service"]: row for row in P.listing()}
    assert listing["trestleiq"]["probe_cost_micro"] == 15_000
    assert listing["wiza"]["probe_cost_micro"] == 0


async def test_trestleiq_paid_probe_rejects_bad_key_and_is_never_saved_as_health_check(
    clients, monkeypatch,
):
    def probe(request):
        assert request.url.path == "/3.0/phone_intel"
        assert request.url.params["phone"] == "+13005550100"
        assert request.url.params["is_sandbox"] == "true"
        if request.headers["x-api-key"] == "bad":
            return httpx.Response(403, json={"errorCode": "AUTHENTICATION_FAILED"})
        return httpx.Response(200, json={"is_valid": False, "warnings": ["Invalid Input"]})

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "trestleiq", "token": "bad"})
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "trestleiq", "token": "own-key"})
        assert good.status_code == 200, good.text
    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "trestleiq")
    assert tool["health_check"] is None


async def test_dropleads_connect_provisions_both_approved_hosts(clients, monkeypatch):
    def probe(request):
        assert request.url.host == "prime.dropleads.io"
        assert request.url.path == "/api/v2/prime-db/credits/balance"
        assert request.headers["x-api-key"] in ("bad", "own-key")
        if request.headers["x-api-key"] == "bad":
            return httpx.Response(401, json={"message": "Invalid API key"})
        return httpx.Response(
            200, json={"success": True, "credits": {"totalAvailable": 0}}
        )

    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        bad = await clients.post(
            "/connections/token", json={"provider": "dropleads", "token": "bad"}
        )
        assert bad.status_code == 422
        good = await clients.post(
            "/connections/token", json={"provider": "dropleads", "token": "own-key"}
        )
        assert good.status_code == 200, good.text

    tools = {tool["name"]: tool for tool in (await clients.get("/tools")).json()}
    assert set(tools) == {"dropleads", "dropleads-contact"}
    assert tools["dropleads"]["base_url"] == "https://prime.dropleads.io"
    assert tools["dropleads-contact"]["base_url"] == "https://api.dropleads.io"
    assert tools["dropleads"]["bindings"] == tools["dropleads-contact"]["bindings"]


# ---- connect-by-key ----------------------------------------------------------------------
async def test_key_connect_provisions_a_header_binding(clients: AsyncClient, monkeypatch):
    """A header key (Apollo's X-Api-Key) is a plain string injected as an env header — never an
    oauth blob with an access_token field that isn't there."""
    # token_verify_field cleared: the generic echo stub doesn't model Apollo's is_logged_in body;
    # this test is about the binding shape, not Apollo's body check (covered separately below).
    monkeypatch.setitem(P.REGISTRY, "apollo", dataclasses.replace(
        P.REGISTRY["apollo"], base_url="http://upstream", probe_path="/whoami", token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "apollo", "token": "sk-apollo"})
    assert r.status_code == 200, r.text
    assert r.json()["health"] == "ok", "a verified key is known-good, not 'unknown'"

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "apollo")
    b = tool["bindings"][0]
    assert b["injector"] == "env" and b["location"] == "header"
    assert b["name"] == "X-Api-Key" and b["format"] == "{secret}"
    assert "secret_field" not in b or b.get("secret_field") in (None, "")


async def test_required_provider_header_is_probed_bound_and_caller_proof(clients: AsyncClient, monkeypatch):
    """A protocol header is provider metadata, not proxy behavior. It must be present during the
    connect probe and become a constant binding that overwrites a caller's stale version."""
    monkeypatch.setitem(P.REGISTRY, "crustdata", dataclasses.replace(
        P.REGISTRY["crustdata"], base_url="http://upstream", probe_path="/requires-version"))
    r = await clients.post("/connections/token", json={"provider": "crustdata", "token": "cr-key"})
    assert r.status_code == 200, r.text

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "crustdata")
    assert tool["bindings"] == [
        {"secret_id": tool["bindings"][0]["secret_id"], "injector": "env", "location": "header",
         "name": "Authorization", "format": "Bearer {secret}"},
        {"secret_id": tool["bindings"][0]["secret_id"], "injector": "env", "location": "header",
         "name": "x-api-version", "format": "2025-11-01"},
    ]
    called = await clients.get(
        "/call/crustdata/requires-version", headers={"x-api-version": "stale-version"})
    assert called.status_code == 200, called.text
    assert called.json()["version"] == "2025-11-01"


async def test_key_connect_supports_a_query_param_key(clients: AsyncClient, monkeypatch):
    """Semrush authenticates the classic API with ?key=… and answers the balance check in PLAIN
    TEXT — the probe must not JSON-parse it, and the tool must bind the key as a query param."""
    monkeypatch.setitem(P.REGISTRY, "semrush", dataclasses.replace(
        P.REGISTRY["semrush"], base_url="http://upstream", probe_url="", probe_path="/units"))
    r = await clients.post("/connections/token", json={"provider": "semrush", "token": "sr-key"})
    assert r.status_code == 200, r.text

    tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "semrush")
    b = tool["bindings"][0]
    assert b["injector"] == "env" and b["location"] == "query"
    assert b["name"] == "key" and b["format"] == "{secret}"


async def test_a_plain_text_error_body_is_rejected(clients: AsyncClient, monkeypatch):
    """Semrush signals a bad key with HTTP 200 + an "ERROR ..." text body. Storing it anyway just
    moves the failure to the first real report call, after the user has left the setup screen."""
    monkeypatch.setitem(P.REGISTRY, "semrush", dataclasses.replace(
        P.REGISTRY["semrush"], base_url="http://upstream", probe_url="", probe_path="/units-bad"))
    r = await clients.post("/connections/token", json={"provider": "semrush", "token": "sr-bad"})
    assert r.status_code == 422, r.text
    assert "ERROR" in r.text
    assert not [c for c in (await clients.get("/connections")).json() if c["provider"] == "semrush"]


async def test_a_200_with_a_false_verify_field_is_rejected(clients: AsyncClient, monkeypatch):
    """Apollo answers HTTP 200 even for a bad key and signals validity in is_logged_in. token_verify_field
    makes us read that field: a false one is rejected, a true one connects. Verified live against Apollo."""
    apollo = dataclasses.replace(
        P.REGISTRY["apollo"], base_url="http://upstream", probe_path="/verify-field",
        token_verify_field="is_logged_in")
    monkeypatch.setitem(P.REGISTRY, "apollo", apollo)

    bad = await clients.post("/connections/token", json={"provider": "apollo", "token": "sk-bad"})
    assert bad.status_code == 422 and "is_logged_in" in bad.text
    assert not [c for c in (await clients.get("/connections")).json() if c["provider"] == "apollo"]

    ok = await clients.post("/connections/token", json={"provider": "apollo", "token": "sk-good"})
    assert ok.status_code == 200, ok.text


async def test_probe_url_overrides_base_url_for_verification(clients: AsyncClient, monkeypatch):
    """The cheapest key-check can live on a different host than the data API (Semrush's balance is on
    www.semrush.com). probe_url must win: point it at a passing endpoint while probe_path would fail."""
    monkeypatch.setitem(P.REGISTRY, "semrush", dataclasses.replace(
        P.REGISTRY["semrush"], base_url="http://upstream",
        probe_url="http://upstream/units", probe_path="/units-bad"))
    r = await clients.post("/connections/token", json={"provider": "semrush", "token": "sr-key"})
    assert r.status_code == 200, "probe_url must be used, not base_url + probe_path"


async def test_key_connection_lists_and_revokes(clients: AsyncClient, monkeypatch):
    """A key connection is a real connection — it must be visible in the list and revocable by id."""
    monkeypatch.setitem(P.REGISTRY, "tikhub", dataclasses.replace(
        P.REGISTRY["tikhub"], base_url="http://upstream", probe_path="/whoami"))
    r = await clients.post("/connections/token", json={"provider": "tikhub", "token": "th-key"})
    assert r.status_code == 200, r.text

    listed = [c for c in (await clients.get("/connections")).json() if c["provider"] == "tikhub"]
    assert len(listed) == 1 and listed[0]["kind"] == "env"
    assert (await clients.delete(f"/connections/{listed[0]['id']}")).status_code == 200


# ---- HTTP Basic providers: paste a raw pair OR a ready-made Base64 blob ----------------------
async def test_basic_provider_encodes_a_raw_login_password_once(clients: AsyncClient, monkeypatch):
    """DataForSEO/Moz take HTTP Basic. Pasting the raw `login:password`, treg Base64s it once and the
    upstream sees `Basic <b64(login:password)>`."""
    import base64
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    r = await clients.post("/connections/token", json={"provider": "dataforseo", "token": "login:pw"})
    assert r.status_code == 200, r.text
    echoed = (await clients.get("/call/dataforseo/whoami")).json()["auth"]
    assert echoed == "Basic " + base64.b64encode(b"login:pw").decode()


async def test_basic_provider_accepts_a_ready_made_base64_blob(clients: AsyncClient, monkeypatch):
    """The DataForSEO and Moz dashboards ALSO hand out a ready-made Base64 credential, and users paste
    that at least as often as the raw pair. treg must NOT Base64 it a second time — the upstream has to
    receive exactly `Basic <the pasted blob>`, decoding back to the original `login:password`."""
    import base64
    blob = base64.b64encode(b"login:pw").decode()
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    r = await clients.post("/connections/token", json={"provider": "dataforseo", "token": blob})
    assert r.status_code == 200, r.text
    echoed = (await clients.get("/call/dataforseo/whoami")).json()["auth"]
    assert echoed == "Basic " + blob, "a pasted Base64 blob must not be double-encoded"


async def test_secret_add_raw_basic_credential_encodes_at_injection(clients: AsyncClient, monkeypatch):
    """Secrets added via `treg secret add dataforseo` bypass the connect flow and store the raw value.
    The injector must detect and Base64-encode a raw `login:password` to produce a valid Basic header.

    Regression test for feedback #294 / #276 / #280-282: DataForSEO Lighthouse returned 40100
    (401 Unauthorized) through treg while the same credential worked via direct curl. The cause
    was that `treg secret add dataforseo --env-var` stored raw `login:password`, but the injector
    used it verbatim as `Basic login:password` instead of `Basic <base64(login:password)>`.
    """
    import base64
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    # Add a raw secret directly, bypassing the /connections/token flow that would Base64-encode it
    r = await clients.post("/secrets", json={"name": "dataforseo", "value": "login:pw"})
    assert r.status_code == 200, r.text
    # Call a DataForSEO catalog endpoint to trigger marketplace resolution with the named secret
    # (the named-tool path won't find a tool; this exercises _marketplace_secret -> _provider_bindings)
    echoed = (await clients.get("/call/dataforseo.account.usage")).json()["auth"]
    expected = "Basic " + base64.b64encode(b"login:pw").decode()
    assert echoed == expected, f"raw secret must be Base64-encoded at injection time, got {echoed!r}"


async def test_secret_add_already_encoded_basic_credential_not_double_encoded(clients: AsyncClient, monkeypatch):
    """Secrets added with an already-encoded Base64 blob must not be double-encoded by the injector."""
    import base64
    blob = base64.b64encode(b"login:pw").decode()
    monkeypatch.setitem(P.REGISTRY, "dataforseo", dataclasses.replace(
        P.REGISTRY["dataforseo"], base_url="http://upstream", probe_path="/whoami"))
    # Add an already-encoded secret directly
    r = await clients.post("/secrets", json={"name": "dataforseo", "value": blob})
    assert r.status_code == 200, r.text
    # Call a DataForSEO catalog endpoint to trigger marketplace resolution with the named secret
    echoed = (await clients.get("/call/dataforseo.account.usage")).json()["auth"]
    assert echoed == "Basic " + blob, f"already-encoded secret must not be double-encoded, got {echoed!r}"


async def test_probe_parses_json_body_labelled_text_plain(clients: AsyncClient, monkeypatch):
    """ScrapeCreators answers HTTP 200 with a JSON body but a text/plain content-type; validity lives
    in creditCount. Gating the parse on application/json left the field unread and refused a good key."""
    monkeypatch.setitem(P.REGISTRY, "scrapecreators", dataclasses.replace(
        P.REGISTRY["scrapecreators"], base_url="http://upstream",
        probe_path="/credit-json-as-text", token_verify_field="creditCount"))
    r = await clients.post("/connections/token", json={"provider": "scrapecreators", "token": "sc-key"})
    assert r.status_code == 200, r.text


async def test_probe_keeps_a_query_string_baked_into_probe_path(clients: AsyncClient, monkeypatch):
    """A probe_path like `/autocomplete?field=title` must reach the upstream WITH that query — httpx
    drops a URL's own query when params= is passed, which used to 400 the probe and refuse the key."""
    monkeypatch.setitem(P.REGISTRY, "pdl", dataclasses.replace(
        P.REGISTRY["pdl"], base_url="http://upstream",
        probe_path="/needs-query?field=title&text=data", token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "pdl", "token": "pdl-key"})
    assert r.status_code == 200, r.text


async def test_query_token_survives_alongside_a_probe_path_query(clients: AsyncClient, monkeypatch):
    """A query-credential provider (SpyFu: ?api_key=) whose probe_path ALSO carries a required query
    (?domain=) must send both — the merge keeps the path's params and adds the credential on top."""
    monkeypatch.setitem(P.REGISTRY, "spyfu", dataclasses.replace(
        P.REGISTRY["spyfu"], base_url="http://upstream",
        probe_path="/needs-query?field=title", token_verify_field=""))
    r = await clients.post("/connections/token", json={"provider": "spyfu", "token": "spyfu-secret"})
    assert r.status_code == 200, r.text


# ---- ContactOut ----


@pytest.mark.parametrize("status", [200, 402])
async def test_financialdatasets_connect_accepts_only_valid_key_outcomes_without_health_probe(
    clients, monkeypatch, status,
):
    """200 and 402 prove the key reached the prepaid account.

    The absolute connect-only probe must also stay out of the saved Tool health metadata: replaying
    a paid data request from recurring health would consume Credits.
    """
    def probe(request):
        assert request.url.path == "/prices/snapshot"
        assert request.url.params["ticker"] == "AAPL"
        assert request.headers["X-API-KEY"] == "valid-empty-key"
        return httpx.Response(status, json={"detail": "Insufficient credits"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        response = await clients.post(
            "/connections/token",
            json={"provider": "financialdatasets", "token": "valid-empty-key"},
        )
        assert response.status_code == 200, response.text
        tool = next(t for t in (await clients.get("/tools")).json()
                    if t["name"] == "financialdatasets")
        assert tool["health_check"] is None
        assert tool["bindings"][0]["name"] == "X-API-KEY"
