"""Old-client failures must stop before credentials or team selection can change."""
import httpx
import pytest

from treg.api import app
from treg.domain.identity import session as sess

OLD_CLI = {'ngrok-skip-browser-warning': '1', 'User-Agent': 'python-httpx/0.28.1'}


async def test_old_email_flow_is_refused_before_otp_or_token_issue(clients):
    for path, body in [('/auth/email/start', {'email': 'new@example.org'}),
                       ('/auth/email/verify', {'email': 'new@example.org', 'code': '123456'})]:
        response = await clients.post(path, json=body, headers=OLD_CLI)
        assert response.status_code == 426
        assert 'treg update' in response.json()['detail']
        assert 'token' not in response.json()
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Treg-Error'] == '1'
        assert 'set-cookie' not in response.headers


@pytest.mark.parametrize('headers', [
    {**OLD_CLI, 'X-Treg-Key-Protocol': '1'},
    {**OLD_CLI, 'User-Agent': 'Mozilla/5.0'},
    {'User-Agent': 'python-httpx/0.28.1'},
])
async def test_current_cli_browser_and_generic_api_keep_email_login(clients, headers):
    response = await clients.post('/auth/email/start', json={'email': 'new@example.org'}, headers=headers)
    assert response.status_code == 200


async def test_old_team_change_rejected_before_creating_team_or_consuming_invite(clients):
    before = (await clients.get('/orgs')).json()
    for path, body in [('/orgs', {'name': 'Must not exist'}),
                       ('/invites/accept', {'email': 'new@example.org', 'code': 'unused'}),
                       ('/invites/999/accept', {})]:
        response = await clients.post(path, json=body, headers=OLD_CLI)
        assert response.status_code == 426
        assert response.headers['X-Treg-Error'] == '1'
    assert (await clients.get('/orgs')).json() == before
    assert (await clients.get('/tools')).status_code == 200


async def test_protocol_hint_cannot_relax_team_or_bootstrap_restrictions(clients):
    token = clients.headers['X-Treg-Token']
    claims = sess.read_identity_claims(token)
    second = (await clients.post('/orgs', json={'name': 'Other'})).json()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://registry') as c:
        for hint in ('', '1'):
            headers = {'X-Treg-Key-Protocol': hint, 'X-Treg-Org': second['org'],
                       'X-Treg-Token': token}
            assert (await c.get('/tools', headers=headers)).status_code == 403
            headers['X-Treg-Token'] = sess.make_identity(
                claims['uid'], scope=sess.BOOTSTRAP_SCOPE, ttl=sess.BOOTSTRAP_TTL_SECONDS)
            assert (await c.get('/tools', headers=headers)).status_code == 403
            assert (await c.get('/auth/cli-token', headers=headers)).status_code == 403
