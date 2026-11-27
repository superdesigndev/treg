"""The CLI's account commands, run unmodified against this checkout's ASGI server.

No CLI config, browser, paid upstream call, or production service is used.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx

from treg import cli
from treg import email as email_sender
from treg.api import app
from treg.domain.identity import session as sess


async def test_login_team_switch_team_create_and_email_login_each_leave_a_working_key(
        clients, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'CONFIG_PATH', tmp_path / 'config.json')
    monkeypatch.setattr(cli, '_maybe_offer_onboarding', lambda cfg: None)
    # The sync CLI runs on a thread; each HTTP request reaches the real async server on
    # the fixture loop. The normal httpx cookie and header handling remains in place.
    loop = asyncio.get_running_loop()
    trace = []
    otp = {}

    async def deliver(email, code, **kwargs):  # the inbox, where no dev code is shown
        otp['code'] = code

    monkeypatch.setattr(email_sender, 'send_otp', deliver)

    async def send(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
            response = await c.send(request)
            trace.append((request.method, request.url.path, response.status_code))
            if request.url.path == '/auth/email/start' and response.status_code == 200:
                otp['code'] = response.json().get('dev_code') or otp['code']
            if request.url.path == '/auth/cli/start' and response.status_code == 200:
                otp['pair'] = response.json()['code']
            return httpx.Response(response.status_code, headers=response.headers,
                                  content=response.content, request=request)

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            return asyncio.run_coroutine_threadsafe(send(request), loop).result(timeout=20)

    class Client(httpx.Client):
        def __init__(self, **kw):
            kw['transport'] = Transport()
            super().__init__(**kw)

    def request(method, url, **kw):
        with Client() as c:
            return c.request(method, url, **kw)

    httpx_api = {k: v for k, v in vars(httpx).items() if k not in {"Client", "post", "get"}}
    monkeypatch.setattr(cli, "httpx", SimpleNamespace(
        **httpx_api, Client=Client,
        post=lambda url, **kw: request('POST', url, **kw),
        get=lambda url, **kw: request('GET', url, **kw),
    ))
    # _RegistryClient was defined before the test transport was installed.
    monkeypatch.setattr(cli, '_RegistryClient', Client)
    monkeypatch.setattr('builtins.input', lambda prompt='': otp['code'])
    # Fixture's owner already has a team and a valid browser session can be derived in test.
    owner_token = clients.headers['X-Treg-Token']
    claims = sess.read_identity_claims(owner_token)
    cookie = sess.make_session(claims['uid'])
    team = (await clients.get('/orgs')).json()[0]['slug']
    second = (await clients.post('/orgs', json={'name': 'Second'})).json()['org']

    def browser_open(url):
        parsed = urlsplit(url)
        lid = parse_qs(parsed.query)['cli'][0]
        response = request('POST', 'http://test/auth/cli/approve',
                           json={'login_id': lid, 'code': otp['pair'], 'org': team},
                           headers={'Cookie': f'treg_session={cookie}'})
        assert response.status_code == 200, response.text
        return True

    monkeypatch.setattr(cli, "webbrowser", SimpleNamespace(open=browser_open))
    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=lambda seconds: None))
    cfg = {'base_url': 'http://test', 'token': None}
    parser = cli.build_parser()

    async def command(*argv):
        args = parser.parse_args(argv)
        return await asyncio.to_thread(args.fn, args, cfg)

    async def resources():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            r = await c.get('/tools', headers={'X-Treg-Token': cfg['token'], 'X-Treg-Org': cfg['active_org']})
            return r.status_code

    outcomes = {}
    await command('login')
    outcomes['browser_login'] = await resources()
    await command('org', 'use', second)
    outcomes['team_switch'] = await resources()
    await command('org', 'create', 'Browser-created')
    outcomes['browser_team_create'] = await resources()
    # An existing saved unscoped identity token from the old server.
    cfg.update(token=sess.make_identity(claims['uid']), identity=True, active_org=team)
    outcomes['saved_token'] = await resources()
    email = (await clients.get('/auth/me')).json()['email']
    await command('login', '--email', email)
    outcomes['email_login'] = await resources() if cfg.get('active_org') else 'no team'
    await command('org', 'create', 'Email-created')
    outcomes['email_team_create'] = await resources()
    assert outcomes == dict.fromkeys(outcomes, 200), (outcomes, trace)
