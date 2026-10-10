"""Roster's bounded data-key surface and free connect-time probe."""
import httpx
from httpx import AsyncClient
from treg.api import app
from treg import oauth_providers as P


def test_roster_data_key_registration():
    p = P.REGISTRY['roster']
    assert p.auth_kind == 'key'
    assert p.base_url == 'https://api.ugcroster.com/v1'
    assert p.probe_path == '/usage'
    assert p.token_header == 'Authorization'
    assert p.token_format == 'Bearer {secret}'
    assert P.is_configured(p)


async def test_roster_connect_uses_free_usage_probe(clients, monkeypatch):
    def probe(request):
        assert request.method == 'GET'
        assert request.url.path == '/v1/usage'
        assert request.headers['authorization'] == 'Bearer own-key'
        return httpx.Response(200, json={'creditsUsed': 0})
    async with AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        response = await clients.post('/connections/token', json={'provider': 'roster', 'token': 'own-key'})
    assert response.status_code == 200, response.text


async def test_roster_connect_rejects_invalid_key(clients, monkeypatch):
    async with AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        401, json={'error': 'Invalid or revoked API key', 'code': 'INVALID_API_KEY'}
    ))) as upstream:
        monkeypatch.setattr(app.state, 'http', upstream)
        response = await clients.post('/connections/token', json={'provider': 'roster', 'token': 'bogus-key'})
    assert response.status_code == 422, response.text
