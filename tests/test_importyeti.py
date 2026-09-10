"""Draft listing contracts; fixtures do not stand in for paid upstream verification."""

import httpx

from treg import oauth_providers as providers
from treg.api import app
from treg.config import Settings
from treg.domain.catalog import store


async def test_importyeti_rejects_bad_key_and_binds_verified_key(clients, monkeypatch):
    def upstream_reply(request):
        assert request.url.host == "data.importyeti.com"
        assert request.url.path == "/v1.0/database-updated"
        assert "authorization" not in request.headers
        if request.headers["IYApiKey"] == "bad-key":
            # Observed live on 2026-09-10.
            return httpx.Response(401, json={"message": "Unauthorized", "statusCode": 401})
        assert request.headers["IYApiKey"] == "test-importyeti-key"
        # Synthetic success: connect must not require a positive paid credit balance.
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(b'{"data": {}, "creditsRemaining": 0}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream_reply)) as upstream:
        monkeypatch.setattr(app.state, "http", upstream)
        rejected = await clients.post("/connections/token", json={
            "provider": "importyeti", "token": "bad-key"})
        assert rejected.status_code == 422
        assert "HTTP 401" in rejected.text
        assert not (await clients.get("/tools")).json()

        accepted = await clients.post("/connections/token", json={
            "provider": "importyeti", "token": "test-importyeti-key"})
        assert accepted.status_code == 200, accepted.text
        tool = next(t for t in (await clients.get("/tools")).json() if t["name"] == "importyeti")
        binding = tool["bindings"][0]
        assert binding["location"] == "header"
        assert binding["name"] == "IYApiKey"
        assert binding["format"] == "{secret}"
        called = await clients.get("/call/importyeti/v1.0/database-updated", headers={
            "IYApiKey": "caller-supplied-key"})
        assert called.status_code == 200, called.text


def test_importyeti_platform_slot_cannot_enable_unvalidated_spend(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_IMPORTYETI", "test-platform-key")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "importyeti")
    assert Settings(_env_file=None).platform_key_for("importyeti") == "test-platform-key"
    assert providers.platform_bindings(providers.get("importyeti")) == [
        {"platform_setting": "platform_key_importyeti", "injector": "env",
         "location": "header", "name": "IYApiKey", "format": "{secret}"}]
    cat = store.load()
    endpoints = [ep for ep in cat.endpoints if ep["provider"] == "importyeti"]
    assert len(endpoints) == 15
    assert cat.credit_rates["importyeti"] is None
    for ep in endpoints:
        assert not ep["verified"]
        assert not cat.platform_eligible(ep)
    # Even entering a rate later must not bypass the remaining live/billing gates.
    monkeypatch.setitem(cat.credit_rates, "importyeti", 0.01)
    assert all(not cat.platform_eligible(ep) for ep in endpoints)


def test_supplier_discovery_does_not_advertise_conditional_search_as_free():
    cat = store.load()
    for role in ("suppliers", "importers"):
        ep = cat.by_id[f"importyeti.trade.{role}.search"]
        assert ep["cost"]["value"] is None
        assert cat.cost_view(ep["cost"], "importyeti")["usd"] is None
    results, _ = store.search("suppliers shipment", cat)
    assert any(ep["provider"] == "importyeti" for ep, _ in results)
